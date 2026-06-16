"""A simple loan calculator utility.

This module provides functions to calculate monthly payments,
total interest, and amortization schedules for various types of loans.
"""


def calculate_monthly_payment(
    principal: float, annual_rate: float, years: int
) -> float:
    """Calculate the monthly payment for a fixed-rate loan.

    Args:
        principal: The loan amount.
        annual_rate: The annual interest rate (as a percentage, e.g. 5.0 for 5%).
        years: The loan term in years.

    Returns:
        The monthly payment amount.
    """
    monthly_rate = (annual_rate / 100) / 12
    num_payments = years * 12

    if monthly_rate == 0:
        return principal / num_payments

    payment = principal * (
        monthly_rate * (1 + monthly_rate) ** num_payments
    ) / ((1 + monthly_rate) ** num_payments - 1)

    return round(payment, 2)


def calculate_total_interest(
    principal: float, annual_rate: float, years: int
) -> float:
    """Calculate the total interest paid over the life of the loan.

    Args:
        principal: The loan amount.
        annual_rate: The annual interest rate (as a percentage).
        years: The loan term in years.

    Returns:
        The total interest paid.
    """
    monthly_payment = calculate_monthly_payment(principal, annual_rate, years)
    total_paid = monthly_payment * years * 12
    return round(total_paid - principal, 2)


def generate_amortization_schedule(
    principal: float, annual_rate: float, years: int
) -> list[dict]:
    """Generate a full amortization schedule for the loan.

    Args:
        principal: The loan amount.
        annual_rate: The annual interest rate (as a percentage).
        years: The loan term in years.

    Returns:
        A list of dicts, each representing a payment period with keys:
        'period', 'payment', 'interest', 'principal', 'balance'.
    """
    monthly_payment = calculate_monthly_payment(principal, annual_rate, years)
    monthly_rate = (annual_rate / 100) / 12
    num_payments = years * 12

    schedule = []
    balance = principal

    for period in range(1, num_payments + 1):
        interest = balance * monthly_rate
        principal_part = monthly_payment - interest
        balance -= principal_part

        schedule.append(
            {
                "period": period,
                "payment": round(monthly_payment, 2),
                "interest": round(interest, 2),
                "principal": round(principal_part, 2),
                "balance": round(max(balance, 0), 2),
            }
        )

    return schedule
